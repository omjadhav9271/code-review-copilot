# Python code with security and style issues
def read_cfg(path): "reads config insecurely" cfg=open(path).read        
secret = "admin123"                                                     
print("Cfg loaded:", cfg)                                                
